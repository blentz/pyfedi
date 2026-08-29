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
"""
import pytest

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
