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

OrderedCollection exemption -- a finding, not a test of correct behaviour
-------------------------------------------------------------------------
`object_has_missing_fields` (app/activitypub/util.py:4659-4663) returns
False for ANY object typed `OrderedCollection`, unconditionally:

    if 'type' in object and object['type'] == 'OrderedCollection':
        return False

No other key is checked. So an Announce whose object is `{'type':
'OrderedCollection'}` -- no `id`, no `actor`, no `object` -- passes the
field check at routes.py:657 and falls through. The very next line inside
that same `if request_json['type'] == 'Announce'...` block that still
executes is routes.py:671, `id = object['id']`, which raises `KeyError:
'id'` because this object has no `id` key at all. Flask's test app runs with
`TESTING = True` and no `PROPAGATE_EXCEPTIONS` override, so the exception
propagates out of `client.post(...)` rather than being turned into a 500
response -- confirmed by running the test below, not assumed. The test
demonstrates reachability (a `pytest.raises(KeyError)` around the POST) and
is deliberately NOT written as a refusal test: there is no `exception_
message` to assert here, because `log_incoming_ap` is never reached on this
path. This is an unhandled-exception defect for the register (Task 8), not
something this sub-project fixes.

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
"""
import pytest

from app import db
from app.constants import ALLOWLIST_STRONG
from app.models import ActivityPubLog, Site
from tests.factories import inbox_activity, signed_inbox_post

pytestmark = pytest.mark.usefixtures('redis_double')


def test_an_unparseable_body_is_refused(app, site):
    with app.test_client() as client:
        response = client.post('/inbox', data='{not json',
                               content_type='application/json')

    assert response.status_code == 400


def test_a_json_null_body_is_refused(app, site):
    """`request.get_json` returns None rather than raising for a bare null."""
    with app.test_client() as client:
        response = client.post('/inbox', data='null',
                               content_type='application/json')

    assert response.status_code == 400


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


def test_an_announce_of_an_ordered_collection_is_not_refused_by_the_field_check(app, signing_peer):
    """FINDING, not a refusal test: object_has_missing_fields returns False
    for ANY OrderedCollection-typed object without checking id/actor/object
    (app/activitypub/util.py:4661-4662), so this Announce -- wrapping an
    object with no id, no actor, no object, nothing but 'type' -- is not
    caught at routes.py:657 and falls through. The very next statement still
    inside that Announce branch, routes.py:671 (`id = object['id']`), then
    raises KeyError, because this object has no 'id' either. There is no
    exception_message to assert here: log_incoming_ap is never reached on
    this path, and TESTING=True (no PROPAGATE_EXCEPTIONS override) lets that
    KeyError propagate out of the test client rather than becoming a 500.
    Reported for the defect register (Task 8), not fixed here.
    """
    activity = inbox_activity(signing_peer, activity_type='Announce', object={'type': 'OrderedCollection'})

    with app.test_client() as client:
        with pytest.raises(KeyError):
            client.post('/inbox', json=activity)


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
    site = Site.query.get(1)
    site.allowlist_mode = ALLOWLIST_STRONG
    db.session.commit()
    activity = inbox_activity(signing_peer)

    with app.test_client() as client:
        response = client.post('/inbox', json=activity)

    assert response.status_code == 403
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
                        lambda *args: dispatched.append(args))
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
