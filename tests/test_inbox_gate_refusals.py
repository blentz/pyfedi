"""shared_inbox (app/activitypub/routes.py) is the endpoint every remote
instance's POST to /inbox, /site_inbox, /u/<actor>/inbox and /c/<actor>/inbox
resolves to. This file covers the REFUSALS THAT HAPPEN BEFORE ANY SIGNATURE
IS CHECKED: an unparseable body, a JSON `null` body, and the two federation-
pause states read from Redis. Task 1 built the signing lever this
sub-project needs for everything past this point; these four tests are
deliberately unsigned, because none of them reach signature verification.

STEP 1 -- deriving the gate's shape, and checking it against the design
=========================================================================

Command (run against this checkout's app/activitypub/routes.py):

    podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
    import ast
    src = open('app/activitypub/routes.py').read()
    n = next(x for x in ast.walk(ast.parse(src))
             if isinstance(x, ast.FunctionDef) and x.name == 'shared_inbox')
    print('span', n.lineno, n.end_lineno)
    print('If:', len([x for x in ast.walk(n) if isinstance(x, ast.If)]))
    print('Try:', len([x for x in ast.walk(n) if isinstance(x, ast.Try)]))
    print('Return:', len([x for x in ast.walk(n) if isinstance(x, ast.Return)]))
    for r in [x for x in ast.walk(n) if isinstance(x, ast.Return)]:
        print('  line', r.lineno, ':', ast.unparse(r))
    "

Output:

    span 625 763
    If: 21
    Try: 4
    Return: 18
      line 763 : return ''
      line 638 : return ('', 400)
      line 642 : return ('', 429)
      line 652 : return ('', 200)
      line 679 : return ('', 200)
      line 685 : return ''
      line 708 : return ('', 200)
      line 756 : return ''
      line 631 : return ('', 400)
      line 634 : return ('', 400)
      line 644 : return ('', 410)
      line 663 : return ('', 200)
      line 669 : return ('', 200)
      line 675 : return ('', 403)
      line 691 : return ('', 400)
      line 701 : return ('', 200)
      line 739 : return ('', 400)
      line 725 : return ('', 400)

Comparison against the design's twenty-row table
(docs/superpowers/specs/2026-08-28-coverage-inbox-gate-design.md, "Scope"):

The table's row order matches source order exactly, and every row's stated
outcome (status code, or the log category implied by its description) matches
what the code at the cited line does. But the table's twenty rows map onto
only 18 return statements, and the arithmetic behind that is worth stating
plainly because it is exactly what a later task would get wrong by counting
returns instead of reading conditions:

- **Rows 7 and 8 share a single return statement (line 663).** Both are the
  `Announce` object-has-missing-fields branch; they differ only in which
  `log_incoming_ap` call executes (`'Intended for Mastodon'` for a `Page`/
  `Note` object, a failure log for any other type), and both fall through to
  the same `return '', 200`. A test distinguishing row 7 from row 8 cannot do
  it from the return site or the status code -- it has to assert the log
  category/message, exactly as the design's own quality-bar section says a
  200-outcome test must.
- **Rows 18 and 19 have no return statement at all.** Both are "verification
  failed, but an exemption applies" outcomes inside the `HttpSignature.
  verify_request` `except VerificationError` block: row 18 (valid LD
  signature) falls out of the inner `try/except` with no return, and row 19
  (fediseer `ChatMessage` exemption) falls out of the `elif` with no return
  either (`...` is the entire body of that branch). Both continue into the
  shared instance-bookkeeping code and the dispatch that is row 20. This is
  consistent with the design's separate note that the two dispatch paths
  (`process_delete_request` / `process_inbox_request`) come after the table,
  not as though the table were missing two returns.
- **Row 20 ("success") is itself two return statements**, not one: line 756
  (`return ''` after `process_delete_request`/`.delay()` for the account-
  deletion path) and line 763 (`return ''` after `process_inbox_request`/
  `.delay()` for every other type). The design's separate paragraph about
  "the two dispatch paths ... each split again on `current_app.debug`"
  already covers this -- it is not a gap, just confirmation that the table
  deliberately collapses what the AST shows as two sites.

Net: 20 rows - 2 (rows 18/19, no return) - 1 (rows 7/8 collapse to one
return) + 1 (row 20 expands to two returns) = 18, which is exactly the
AST's count. **No row's condition or stated outcome is wrong.** The one
substantive addition this derivation makes: row 19 (fediseer exemption)
carries the same `bounced = True` consequence as row 18, because both are
reached through the same `except VerificationError` block that sets
`bounced = True` unconditionally at its top (line 718) before branching on
`'signature' in request_json`. The table states "bounced true" only for row
18; it is equally true for row 19, and it is what drives the `ip_address`
blank-out this sub-project's design calls out as in-scope. This is a
completion, not a correction -- the design never claims row 19 does NOT
bounce, it just doesn't say it does.

One further confirmation, not a disagreement: the design's quality-bar
section says "a 200 is returned by six different outcomes in this gate."
The table lists SEVEN 200-outcome rows (6, 7, 8, 9, 11, 14, 15), but rows 7
and 8 share one return statement as described above, so there are exactly
six return SITES producing `('', 200)` (lines 652, 663, 669, 679, 701, 708).
The "six" in the quality-bar text is counting return sites, not table rows,
and both countings are internally consistent once that distinction is made
explicit.

STEP 4 -- the BlockingIOError arm (design table row 2)
=========================================================================

Tried, and it does not survive contact with this Werkzeug version (3.1.8),
for a specific and checkable reason rather than mere test-client
awkwardness:

`request.get_json(force=True)` calls `Request.get_data()`, which calls
`self.stream.read()`. `self.stream` is a `werkzeug.wsgi.LimitedStream`
wrapping `environ['wsgi.input']` (`werkzeug/wsgi.py`'s `get_input_stream`,
used unconditionally to build `Request.stream` -- not a test-client-only
path). `LimitedStream.readinto()` (`werkzeug/wsgi.py:534`) wraps every read
of the underlying stream in `try: ... except (OSError, ValueError) as e:
self.on_disconnect(error=e)`, and `BlockingIOError` is an `OSError`
subclass. `on_disconnect` raises `werkzeug.exceptions.ClientDisconnected`,
which is a subclass of `werkzeug.exceptions.BadRequest` -- not
`BlockingIOError`. So a stream that raises `BlockingIOError` while
`shared_inbox` is parsing the body surfaces to application code as a
`BadRequest`, and is caught by the FIRST except clause (line 629's `except
werkzeug.exceptions.BadRequest`), never by the second (line 632's `except
BlockingIOError`).

This was verified experimentally, not just read: a throwaway test posted to
`/inbox` with `environ_overrides={'wsgi.input': <a stream whose .read()
raises BlockingIOError>}`. It returned 400, and with
`LOG_ACTIVITYPUB_TO_DB` enabled the `ActivityPubLog.exception_message` read
"Unable to parse json body: ..." -- the BadRequest branch's message, not
"Client disconnected while sending JSON body ..." from the BlockingIOError
branch. Both a `Content-Length` larger than the body sent and a
`Content-Length` shorter than the body sent were also tried directly (no
custom stream); Werkzeug's test-client `EnvironBuilder` recomputes
`CONTENT_LENGTH` from the actual body bytes regardless of what header is
passed, so both attempts round-tripped to a normal 200 and touched neither
except clause.

Because the interception happens in Werkzeug's own stream-reading layer,
common to every WSGI deployment of this Werkzeug version and not a
test-client artifact, `except BlockingIOError:` in `shared_inbox`
(app/activitypub/routes.py:632-634) appears to be dead code under Werkzeug
3.1.8 -- it would only fire for something that raises `BlockingIOError`
from a context `LimitedStream` does not mediate, which is not how
`request.get_json()` reaches the socket. This is left UNCOVERED rather than
given a test that hits 400 through the sibling branch while claiming to
pin this one; that would be exactly the vacuous-status-code-only test this
sub-project's quality bar rules out, just relocated to the docstring instead
of the assertion. Reported as a finding for the register, not fixed here
(out of scope per the design's "Fixing anything" rule).

STEP 2/3 -- the four tests below
=========================================================================

Run: ./run_tests.sh tests/test_inbox_gate_refusals.py -q --no-cov
Result: 4 passed.

The two pause-federation tests deliberately post a body that would also fail
the minimum-field check (`{"a": 1}`), which is the point: it proves the pause
switch is read and acted on BEFORE that check, not merely that a paused
instance eventually returns something other than 200. `redis_double` gives
`app.redis_client` a real `fakeredis.FakeRedis(decode_responses=True)`
instance, so `redis_client.get('pause_federation')` returns `str`, and the
production comparisons `pause_federation == '1'` / `== '666'` held with no
type mismatch -- worth recording since the brief flagged this as a real risk
and it did not materialise here, but a Task 3-5 test using a *different*
Redis client configuration should not assume the same.

`pytestmark` below is this file's own addition, not carried from the brief:
`shared_inbox` reads `pause_federation` from `redis_client` immediately
after the JSON-None check and before the minimum-field check, so every test
here that gets past JSON parsing touches Redis, not just the two that set a
pause value.

TASK 3 -- the field check and the three Announce refusals
=========================================================================
All of Task 3's outcomes (rows 6-9 of the design's table: the minimum-field
check and the three Announce-object checks, routes.py:650-669) return
before `HttpSignature.precheck` (line 687) and before `find_actor_or_create_
cached`/`HttpSignature.verify_request` (lines 703/716), so none of these
tests need a real signature -- a plain `client.post('/inbox', json=activity)`
reaches every branch covered here, exactly like Task 1's four tests above.
`signing_peer` is used anyway (rather than a signature-less actor) because it
is the one fixture this file and test_inbox_gate_signatures.py share, and its
`ap_profile_id` is a convenient, already-valid activity actor URI; its
keypair goes unused by every test below.

`signing_peer` calls `make_site()` itself (tests/conftest.py), so no test
below also requests the `site` fixture -- doing both would create a second
Site row for no benefit, since `g.site = Site.query.get(1)` only ever reads
the first one.

The exact `exception_message` string each outcome logs, verbatim:

  Missing minimum expected fields in JSON                    (row 6)
  Intended for Mastodon                                      (row 7, object
                                                               type Page or
                                                               Note)
  Missing minimum expected fields in JSON Announce object    (row 8, any
                                                               other object
                                                               type)
  Activity about local content which is already present      (row 9)

Rows 7 and 8 are the pair Task 2's AST derivation flagged as sharing one
`return '', 200` (line 663) -- distinguishable only by `exception_message`,
which is exactly what the tests below assert on instead of the status code.

OrderedCollection exemption (D42, fixed)
----------------------------------------
`object_has_missing_fields` exempts an object typed `OrderedCollection` from
the actor/object check, since a collection has neither. It used to exempt it
from the `id` check too, so an Announce of a bare `{'type':
'OrderedCollection'}` passed the field check and then raised `KeyError:
'id'` at `id = object['id']` a few lines later, with no log row. The
exemption now still requires `id`, and such an Announce is refused as row 8.

Note for Tasks 4-5 (this same file): the local-content check (row 9,
routes.py:665-669) sits AFTER `object_has_missing_fields`, so any test of it
must give the Announce object all four fields (`id`, `type`, `actor`,
`object`) or it will be refused at row 7/8 for the wrong reason before ever
reaching the local-content branch -- the brief calls this out explicitly and
it is easy to get backwards.

TASK 4 -- strong-allowlist rejection, duplicate suppression, PeerTube drop
=========================================================================
Source order (routes.py:673-685), all three AFTER the field/Announce checks
above and BEFORE `HttpSignature.precheck`:

  673-675  g.site.allowlist_mode >= ALLOWLIST_STRONG and the actor's host is
           not in AllowedInstances -> ('', 403), NO log_incoming_ap call at
           all -- this is the one outcome in the whole gate that logs
           nothing on refusal.
  677-680  redis_client.exists(id) (an id this process already saw and wrote
           to Redis with a 90s TTL, routes.py:680) -> ('', 200), logged
           'Already aware of this activity'.
  683-685  request_json['actor'] is a string ending 'accounts/peertube' ->
           bare `return ''` -- no status code in the source, unlike every
           sibling return in this gate. Flask defaults a bodyless response
           to 200, so this is observationally identical to ('', 200) from
           outside the process; logged 'PeerTube View or CacheFile
           activity'. Recorded for the register (Task 8): the missing
           status literal is either an oversight or deliberate reliance on
           Flask's default, and nothing in the source says which.

What makes a host "allowed" -- read before writing the allowlist test
-----------------------------------------------------------------------
`instance_allowed(host)` (app/utils.py:2302-2308): None or '' host -> True
(allowed) unconditionally; otherwise it lower-cases/strips the host via
`inbox_domain` and returns whether a matching row exists in the
`AllowedInstances` table (`AllowedInstances.query.filter_by(domain=host)`).
It is a real table lookup, not a config value or a comparison against
`g.site` -- so a test does not need to construct a "not allowed" state at
all: `signing_peer` lives on `peer.example`, no fixture anywhere in this
suite inserts an `AllowedInstances` row for it, and `TestConfig.CACHE_TYPE
= 'NullCache'` means `@cache.memoize(150)` on `instance_allowed` never
serves a stale answer across tests. Setting `g.site.allowlist_mode =
ALLOWLIST_STRONG` on the existing Site row (id 1, the one `signing_peer`'s
`make_site()` already created) is therefore sufficient by itself -- no
`AllowedInstances` row needs to be created OR absent-by-construction, it is
already absent-by-construction. (An earlier draft of this test guessed the
condition was about `g.site` state alone and would have passed for the
wrong reason had `AllowedInstances` happened to carry a wildcard-style
entry; it does not, so this note also serves as the check that the guess
was right.)

The duplicate test signs BOTH requests, deliberately, per the brief. The
duplicate-id check (677-680) sits AFTER the field check but BEFORE
`HttpSignature.precheck`/`verify_request` (688/716) -- so this check does not
itself need a valid signature, and an UNSIGNED first POST would reach it,
write the id to Redis, and return 200 just as readily as a signed one. That
is exactly the trap: with unsigned posts, the SECOND request would also
return 200, but for the wrong reason if signature verification were ever
moved ahead of the duplicate check, or if some other early-exit branch
intervened -- the test would keep passing while proving nothing about
duplicate suppression specifically. Signing both requests, and asserting the
`process_inbox_request` dispatch recorder fired exactly once AND the second
row's `exception_message` is 'Already aware of this activity', pins the
behaviour to the actual branch rather than to any 200-returning outcome that
happens to come first.

TASK 5 -- precheck, the Delete shortcut, and actor resolution
=========================================================================
Source order (routes.py:687-708), all three AFTER Task 4's checks above and
all three still BEFORE `HttpSignature.verify_request` (716) -- the actual
cryptographic signature check this file's docstring at the top calls out as
"never patched anywhere in this suite":

  687-691  `HttpSignature.precheck(request)` (app/activitypub/signature.py:
           380-395) raises `VerificationFormatError` -> ('', 400), logged
           'Precheck failed: ' + str(e).
  693-701  A `Delete` whose `object` is a string equal to `actor`, for an
           actor with no matching `User` row -> ('', 200), logged 'Does not
           exist here', account_deletion set True but never acted on.
  703-708  Any other activity's actor, unresolvable by
           `find_actor_or_create_cached` -> ('', 200), logged
           f'Actor could not be found 1 - : {actor_name}, actor object: None'.

What `HttpSignature.precheck` actually checks -- read before writing a test
for it
-----------------------------------------------------------------------
`precheck` (app/activitypub/signature.py:380-395) does not read the
`Signature` header at all. It checks exactly two things: a `Digest` header
present and equal to `HttpSignature.calculate_digest(request.data)`, and a
`Date` header present and within 3600 seconds of now. Each failure raises
`VerificationFormatError` with its own message ("No digest header present" /
"Digest is incorrect" / "No date header present" / "Date is too far away").
This matters for which input trips it: a malformed `Signature` header does
not touch precheck's code at all -- it is only ever read later, inside
`HttpSignature.verify_request` (398-420), which this file never patches or
exercises with a bad signature (test_inbox_gate_signatures.py covers that).

The test below trips precheck through the Digest-mismatch arm, using
`signed_inbox_post`'s own documented escape hatch rather than a hand-rolled
header: its `body` parameter "overrides the bytes actually sent while
leaving the signature alone... how a test produces a request whose digest no
longer matches its body" (tests/factories.py). A real signed request is
built normally, so its `Digest` header is a correctly-computed value for the
ORIGINAL body; a differently-serialised body -- the same four required keys
plus one extra, so the bytes differ -- is substituted on the wire.
`request.data` is the substituted bytes, `request.headers['digest']` is
still the original header, and `HttpSignature.calculate_digest` runs exactly
as production always calls it, on real bytes -- the test only arranges for
the two sides of that comparison to disagree, which is externally identical
to a body corrupted in transit.

The Delete-shortcut test is deliberately UNSIGNED -- no `Signature` header at
all -- because 693-701 is reached before `HttpSignature.verify_request`
(716), and the brief calls this out explicitly: an unsigned request reaching
a 200 there is exactly the kind of thing worth proving directly. It is NOT
reached before `precheck` (687), though, which sits earlier in source order
than the Delete check -- so "unsigned" here means what it means throughout
this gate (no cryptographic Signature header, since nothing before line 716
ever reads one), not "no headers at all": the test still supplies a correct
`Digest` and a fresh `Date`, computed the same way `signed_inbox_post` does
internally (`HttpSignature.calculate_digest`, `werkzeug.http.http_date`), so
the request clears precheck on the way to the shortcut. A request missing
Digest/Date entirely would be refused at 400 by precheck before ever reaching
the Delete check, proving nothing about the Delete shortcut specifically --
confirmed by running exactly that request through precheck's own test above.

The actor-not-found test registers a 404 for the actor's own URI on
`http_mock`, matching how production actually resolves it:
`find_actor_or_create_cached` (app/activitypub/util.py) first tries a
CACHE_TYPE=NullCache-defeated memoized lookup with `create_if_not_found=
False` (no fetch, always a miss here), then falls back to
`find_actor_or_create(actor_url, create_if_not_found=True, ...)`, which -- for
an actor URL no local User/Community/Feed row matches -- calls
`create_actor_from_remote` -> `fetch_remote_actor_data` -> `get_request(url)`,
a single GET to the actor's own URI (app/activitypub/actor.py:132-168). A 404
falls through `fetch_remote_actor_data`'s "any other status code -> give up"
branch and returns None immediately (no retry: only 429/502/503/504 are
retried) -- exactly one fetch, matching `http_mock`'s
`assert_all_called=True`. The activity is signed normally with `signing_peer`,
whose identity is irrelevant here -- `verify_request`, which would check it,
is never reached; only `precheck` runs before line 703, and precheck ignores
the Signature header entirely, per the section above -- while
`activity['actor']` names a wholly different, unresolvable actor. The two are
independent by construction; nothing before line 708 ever compares them.
"""
import json
import uuid

import pytest

from app import db
from app.constants import ALLOWLIST_STRONG
from app.models import ActivityPubLog, Site
from tests.conftest import unsigned_but_precheck_clean_headers
from tests.factories import inbox_activity, signed_inbox_post

pytestmark = pytest.mark.usefixtures('redis_double')


def test_an_unparseable_body_is_refused(app, site, monkeypatch):
    """routes.py:628-631: `request.get_json(force=True)` raises werkzeug's
    `BadRequest` on a malformed body, and the `except werkzeug.exceptions.
    BadRequest` arm catches it, logs 'Unable to parse json body: ...' and
    returns ('', 400).

    The 400 alone is NOT what this test rests on, and an earlier version of
    it that asserted only the status was provably vacuous: deleting the
    whole `except` arm changes the status not at all. `request.get_json`
    raises `BadRequest`, Flask turns an uncaught `HTTPException` into its own
    response, and `app/errors/handlers.py` registers handlers for 404, 500,
    401 and 429 -- but NOT 400 -- so werkzeug's default 400 comes back
    either way. Six other outcomes in this gate also return 400.

    The one thing the arm does that Flask's default does not is call
    `log_incoming_ap`, so that is what is asserted. `log_incoming_ap`
    (app/activitypub/util.py:4471-4486) needs only `LOG_ACTIVITYPUB_TO_DB`
    to write a row -- notably NOT `g.site`, which is not set until
    routes.py:646, well past this arm. The message is suffixed with
    `str(request.user_agent)`, which the test client supplies and this test
    has no reason to pin, hence `startswith` rather than equality.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

    with app.test_client() as client:
        response = client.post('/inbox', data='{not json',
                               content_type='application/json')

    assert response.status_code == 400
    assert ActivityPubLog.query.one().exception_message.startswith('Unable to parse json body: ')


def test_a_json_null_body_is_refused(app, site, monkeypatch):
    """routes.py:636-638: `request.get_json` returns None rather than raising
    for a bare `null`, so the explicit `if request_json is None:` guard --
    not the `except` arm above -- is what refuses it, logging 'Empty JSON
    body ...' and returning ('', 400).

    Deleting that guard does break this test, but only by crashing: the next
    statement to touch `request_json` raises `TypeError`. A status-only
    assertion also cannot tell this 400 apart from the five other 400 sites
    in this gate. Asserting the logged message pins WHICH refusal happened
    and fails by assertion rather than by accident. `startswith` because the
    message carries a `str(request.user_agent)` suffix.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

    with app.test_client() as client:
        response = client.post('/inbox', data='null',
                               content_type='application/json')

    assert response.status_code == 400
    assert ActivityPubLog.query.one().exception_message.startswith('Empty JSON body')


def test_a_paused_instance_returns_429(app, site, redis_double):
    redis_double.set('pause_federation', '1')

    with app.test_client() as client:
        response = client.post('/inbox', data='{"a": 1}',
                               content_type='application/json')

    assert response.status_code == 429


def test_a_closed_instance_returns_410(app, site, redis_double):
    redis_double.set('pause_federation', '666')

    with app.test_client() as client:
        response = client.post('/inbox', data='{"a": 1}',
                               content_type='application/json')

    assert response.status_code == 410


@pytest.mark.parametrize('missing', ['id', 'type', 'actor', 'object'])
def test_a_missing_minimum_field_is_refused(app, signing_peer, monkeypatch, missing):
    """Deleting any one of the four required keys trips the same guard
    (routes.py:650), all the way down to the same log message -- production
    does not distinguish which key was missing, so neither does this test.

    Asserts on `exception_message`, not just the 200: five other outcomes in
    this gate share that status code, so the status code alone would pass
    against a production change that silently swapped in one of them.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    activity = inbox_activity(signing_peer)
    del activity[missing]

    with app.test_client() as client:
        response = client.post('/inbox', json=activity)

    assert response.status_code == 200
    assert ActivityPubLog.query.one().exception_message == 'Missing minimum expected fields in JSON'


@pytest.mark.parametrize('object_type, expected_message', [
    ('Page', 'Intended for Mastodon'),
    ('Note', 'Intended for Mastodon'),
    ('Like', 'Missing minimum expected fields in JSON Announce object'),
])
def test_an_announce_of_a_fieldless_object_is_refused(app, signing_peer, monkeypatch,
                                                       object_type, expected_message):
    """An Announce whose dict object carries only 'type' fails object_has_
    missing_fields (it has no id/actor/object), and which log message fires
    depends solely on that type -- Page and Note are read as Mastodon's
    known-noisy shape and logged as 'Intended for Mastodon', anything else
    (here, Like) gets the generic Announce-object failure message. Both
    outcomes return the SAME '', 200 (routes.py:663, confirmed by Task 2's
    AST derivation), so the message is the only thing that tells them apart.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    activity = inbox_activity(signing_peer, activity_type='Announce', object={'type': object_type})

    with app.test_client() as client:
        response = client.post('/inbox', json=activity)

    assert response.status_code == 200
    assert ActivityPubLog.query.one().exception_message == expected_message


def test_an_announce_of_local_content_is_dropped(app, signing_peer, monkeypatch):
    """The local-content check (routes.py:665-669) sits AFTER object_has_
    missing_fields, so the Announce object here carries all four required
    keys -- if it did not, this would be refused at the row 7/8 branch above
    instead, for the wrong reason, and still return 200 with a DIFFERENT
    message, silently passing a test that got the order backwards.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    local_actor = f"https://{app.config['SERVER_NAME']}/u/localuser"
    local_object = {'id': 'https://remote.example/objects/1', 'type': 'Note',
                    'actor': local_actor, 'object': 'https://remote.example/objects/1'}
    activity = inbox_activity(signing_peer, activity_type='Announce', object=local_object)

    with app.test_client() as client:
        response = client.post('/inbox', json=activity)

    assert response.status_code == 200
    assert ActivityPubLog.query.one().exception_message == 'Activity about local content which is already present'


def test_an_announce_of_an_ordered_collection_with_no_id_is_refused(app, signing_peer, monkeypatch):
    """D42, fixed. object_has_missing_fields exempts OrderedCollection-typed
    objects from the id/actor/object check, because a collection legitimately
    has no actor or object of its own -- but the very next statement in the
    Announce branch, `id = object['id']`, still needs an id. An Announce
    wrapping `{'type': 'OrderedCollection'}` and nothing else used to pass the
    field check and then raise KeyError: 'id' with no log row. The exemption
    now still requires an id, so this is refused with the same logged 200 as
    any other Announce object missing its minimum fields.

    LOG_ACTIVITYPUB_TO_DB is enabled so the one-row assertion is meaningful:
    with the flag at its default of False no refusal writes a row.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    activity = inbox_activity(signing_peer, activity_type='Announce', object={'type': 'OrderedCollection'})

    with app.test_client() as client:
        response = client.post('/inbox', json=activity)

    assert response.status_code == 200
    assert ActivityPubLog.query.one().exception_message == 'Missing minimum expected fields in JSON Announce object'


def test_a_disallowed_actor_is_refused_under_strong_allowlist(app, signing_peer, monkeypatch):
    """routes.py:673-675: with a strong allowlist, an actor whose host has no
    row in AllowedInstances is refused with 403 -- and, uniquely among every
    outcome in this gate, NOTHING is logged. `status_code == 403` alone would
    not distinguish this from a hypothetical future 403 that DID log
    something; `ActivityPubLog.query.count() == 0` (with logging enabled) is
    the assertion that would actually fail if a `log_incoming_ap` call were
    added to this branch, which is exactly the kind of change this test
    exists to catch.

    `instance_allowed` (app/utils.py:2302-2308) is a real AllowedInstances
    table lookup, not a `g.site` flag: `signing_peer` lives on
    'peer.example', and nothing in this suite ever inserts an
    AllowedInstances row for it, so the host is disallowed by construction
    with no extra setup. Only `g.site.allowlist_mode` needs to be raised to
    ALLOWLIST_STRONG (the Site row `signing_peer`'s own `make_site()` call
    already created) to reach this branch. No signature is needed: 673-675
    sits before `HttpSignature.precheck` (688), like every Task 3 outcome.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    site = db.session.get(Site, 1)
    site.allowlist_mode = ALLOWLIST_STRONG
    db.session.commit()
    activity = inbox_activity(signing_peer)

    with app.test_client() as client:
        response = client.post('/inbox', json=activity)

    assert response.status_code == 403
    assert ActivityPubLog.query.count() == 0


def test_a_dict_shaped_actor_is_refused_under_strong_allowlist(app, signing_peer, monkeypatch):
    """REGRESSION test for D47, a live allowlist bypass that is now closed.

    Until 2026-08-29 this test asserted the opposite, as characterisation of
    the defect. Sending `actor` as `{'id': <uri>}` rather than as the bare URI
    string walked straight past routes.py:673-675, because `furl` parses only
    strings and returns `host = None` for a dict, and `instance_allowed(None)`
    then returned True. `find_actor_or_create_cached` unwraps the dict
    (`if isinstance(actor, dict): actor = actor['id']`), so the actor still
    resolved and the activity was dispatched. Measured at the time: 403 with
    zero dispatches for the string form, 200 with one dispatch for the dict.

    **What closed it was not a change at routes.py:673-675.** The fix went into
    the deeper half D47's own row named: `instance_allowed` now returns False
    for an absent host instead of True, so `not instance_allowed(None)` is True
    and the 403 fires. That also closed the same shape everywhere else the pair
    is consulted, which is why it was preferred to reading the host out of the
    dict at this one call site.

    A note on the comparison, corrected from the version this test replaces:
    its docstring claimed the sibling test differed "in exactly one thing, the
    actor's shape". That was imprecise and a reviewer caught it -- the sibling
    posts unsigned via `client.post`, while this one posts a genuinely signed
    request and installs a dispatch recorder. Two variables differ, not one.
    The defect never rested on that comparison; it rested on the three source
    links above, each verified independently, and on the end-to-end measurement.

    Production change that fails this: reverting `instance_allowed`'s empty-host
    answer to True, or otherwise letting a non-string actor reach the gate with
    no host.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))
    site = db.session.get(Site, 1)
    site.allowlist_mode = ALLOWLIST_STRONG
    db.session.commit()
    activity = inbox_activity(signing_peer)
    activity['actor'] = {'id': signing_peer.ap_profile_id}

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)

    assert response.status_code == 403
    assert len(dispatched) == 0
    assert ActivityPubLog.query.count() == 0


def test_a_repeated_activity_is_suppressed(app, signing_peer, monkeypatch, redis_double):
    """routes.py:677-680: an activity id already written to Redis (by an
    earlier delivery of the SAME id) is refused with 200, logged 'Already
    aware of this activity', and -- unlike the first delivery -- never
    reaches `process_inbox_request`. Both posts are signed with
    `signed_inbox_post`: the duplicate check sits after the field check but
    before signature verification, so an unsigned first POST would reach it
    and write the id to Redis just as well, and an unsigned second POST would
    still return 200 -- proving nothing about THIS branch specifically,
    since several other branches in this gate also return bare 200. Signing
    both, and asserting the dispatch count and the exact log message, pins
    the outcome to duplicate suppression rather than to any other
    200-returning path a broken production change might reroute onto.

    One dict, reused for both posts -- not `inbox_activity` called twice --
    because the id must be IDENTICAL for the second post to collide with what
    the first wrote to Redis; `inbox_activity` mints a fresh uuid per call
    specifically so unrelated tests never collide with each other through
    Redis, which is exactly the behaviour this test needs to defeat once, on
    purpose.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))
    activity = inbox_activity(signing_peer)

    with app.test_client() as client:
        first = signed_inbox_post(client, activity, signing_peer)
        second = signed_inbox_post(client, activity, signing_peer)

    assert first.status_code == 200 and second.status_code == 200
    assert len(dispatched) == 1
    assert 'Already aware of this activity' in [
        row.exception_message for row in ActivityPubLog.query.all()]


def test_a_peertube_view_activity_is_dropped(app, signing_peer, monkeypatch):
    """routes.py:683-685: an activity whose 'actor' string ends
    'accounts/peertube' is dropped silently -- logged 'PeerTube View or
    CacheFile activity' -- without ever reaching `HttpSignature.precheck`
    (688), so no signature is needed here either.

    FINDING for the register (Task 8), not fixed here: the source returns
    bare `return ''` at line 685, with no status code literal, unlike every
    other return site in this gate (all of which write `'', <code>`
    explicitly). Flask defaults a bodyless response with no status to 200,
    so `response.status_code == 200` below is observationally identical to
    every other 200-outcome in this file -- but it is Flask's default doing
    the work, not an explicit `200` in the source. A future refactor that
    changed Flask's default handling, or wrapped this return in something
    that no longer defaults to 200, would change this outcome silently; nothing
    in routes.py itself pins the status code the way its neighbours do.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    activity = inbox_activity(signing_peer)
    activity['actor'] = 'https://peer.example/accounts/peertube'

    with app.test_client() as client:
        response = client.post('/inbox', json=activity)

    assert response.status_code == 200
    assert ActivityPubLog.query.one().exception_message == 'PeerTube View or CacheFile activity'


def test_a_tampered_body_fails_precheck(app, signing_peer, monkeypatch):
    """routes.py:687-691: `HttpSignature.precheck` (app/activitypub/
    signature.py:380-395) never reads the Signature header -- only Digest and
    Date -- so the input that trips it is a body whose bytes do not hash to
    the Digest header a real signature already committed to, not a malformed
    Signature header (see the module docstring's TASK 5 section for why that
    first guess is wrong).

    `signed_inbox_post` signs `activity` normally, producing a genuine,
    correct Digest header for its real serialisation. `body=` then substitutes
    a DIFFERENT serialisation on the wire -- the same four required fields
    (so the field check at row 6 still passes first) plus one extra key, so
    the bytes differ from what was actually hashed. Neither `precheck` nor
    `calculate_digest` is patched: production runs both exactly as it always
    does, and the digest genuinely does not match, exactly as it would not for
    a body corrupted after signing.

    `DEBUG=True` is required for `signed_inbox_post` itself to run:
    `HttpSignature.signed_request` validates its OWN destination URI through
    `is_invalid_get_request_uri`, which refuses any `.local` host (this
    suite's `SERVER_NAME`) unless `current_app.debug` is set -- unrelated to
    anything this test is about, but load-bearing for every signed POST to
    `/inbox` in this file.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    activity = inbox_activity(signing_peer)
    tampered_body = json.dumps({**activity, '@context': 'https://www.w3.org/ns/activitystreams',
                                'tamper': 'not what was signed'}).encode('utf8')

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer, body=tampered_body)

    assert response.status_code == 400
    assert ActivityPubLog.query.one().exception_message == 'Precheck failed: Digest is incorrect'


def test_a_delete_of_an_unknown_account_needs_no_signature(app, site, monkeypatch):
    """routes.py:693-701: a `Delete` whose `object` is a string equal to its
    `actor`, for an actor with no matching `User` row, is refused with 200,
    logged 'Does not exist here', and never reaches the dispatch code at the
    bottom of `shared_inbox` (751-756/758-761).

    Deliberately UNSIGNED -- no `Signature` header at all -- because this
    branch sits before `HttpSignature.verify_request` (716), and the brief
    calls out that an unsigned request reaching a 200 here is exactly the
    proof worth having directly, rather than inferred from source reading.
    It is NOT before `HttpSignature.precheck` (687), though, which this
    request still has to clear: a correct `Digest` and a fresh `Date` are
    supplied by `unsigned_but_precheck_clean_headers` (tests/conftest.py),
    which computes them the same way `signed_inbox_post` computes them
    internally, so the request reaches the Delete shortcut rather than being
    refused earlier at precheck for an unrelated reason (see
    test_a_tampered_body_fails_precheck above for what THAT refusal looks
    like, and the module docstring's TASK 5 section for why "unsigned" and
    "clears precheck" are not in tension).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_delete_request',
                        lambda *args, **kwargs: dispatched.append(args))
    ghost = 'https://ghost.example/u/nobody'
    activity = {'id': f'{ghost}/activities/{uuid.uuid4().hex}', 'type': 'Delete',
                'actor': ghost, 'object': ghost}
    body_bytes = json.dumps(activity).encode('utf8')

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 200
    assert ActivityPubLog.query.one().exception_message == 'Does not exist here'
    assert dispatched == []


def test_an_unresolvable_actor_is_refused(app, signing_peer, monkeypatch, http_mock):
    """routes.py:703-708: for any activity that is not the Delete-of-unknown-
    account shortcut above, the actor is resolved through
    `find_actor_or_create_cached`; when that returns None the activity is
    refused with 200, logged f'Actor could not be found 1 - : {actor_name},
    actor object: None', and dispatch never runs.

    `http_mock.get(ghost).respond(404)` registers the ONE fetch production
    actually makes for an unknown actor -- `find_actor_or_create_cached` falls
    back (CACHE_TYPE=NullCache means its memoized lookup is always a miss) to
    `find_actor_or_create(ghost, create_if_not_found=True)`, which calls
    `create_actor_from_remote` -> `fetch_remote_actor_data` -> a single GET to
    the actor's own URI (app/activitypub/actor.py:132-168). A 404 there falls
    through that function's "any other status code -> give up" branch with no
    retry, returning None all the way back up. Registering nothing (or the
    wrong URI) would fail this test at teardown via `http_mock`'s
    `assert_all_called=True`, or as an unmatched-request error instead of the
    404 this test means to exercise -- exactly the distinction the brief
    calls out.

    Signed normally with `signing_peer`, whose identity is irrelevant: only
    `precheck` runs before this branch (703), and precheck never reads the
    Signature header (see the module docstring's TASK 5 section) -- so who
    signed the request and which actor it names are independent here, and the
    test would behave identically if they happened to coincide.

    `DEBUG=True` is required for `signed_inbox_post` itself to run, for the
    same `is_invalid_get_request_uri`/`.local`-host reason given in
    test_a_tampered_body_fails_precheck's docstring above.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))
    ghost = 'https://ghost.example/u/nobody'
    http_mock.get(ghost).respond(404)
    activity = {'id': f'{ghost}/activities/{uuid.uuid4().hex}', 'type': 'Like',
                'actor': ghost, 'object': f'{ghost}/objects/1'}

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)

    assert response.status_code == 200
    assert ActivityPubLog.query.one().exception_message == (
        f'Actor could not be found 1 - : {ghost}, actor object: None')
    assert dispatched == []
