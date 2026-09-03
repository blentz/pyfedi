# Sub-project 11: the ActivityPub content-object endpoints

**Date:** 2026-09-03
**Module:** `app/activitypub/routes.py` — `comment_ap`, `post_ap`,
`post_replies_ap`, `post_ap_context`, `activities_json`, `activity_result`
**Predecessors:** 5a-7 covered the inbox dispatcher and its delegates; 8 the
webfinger discovery surface; 9 the actor-profile endpoints; 10 the collections.
This is the fourth and last large slice of the outbound side.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D187**

## The unit

Six functions, contiguous at `routes.py:2147-2294`.

| Function | Lines at writing | Uncovered |
|---|---|---|
| `post_ap` | 2175-2212 | **24** |
| `post_ap_context` | 2236-2264 | **21** |
| `comment_ap` | 2147-2169 | **16** |
| `post_replies_ap` | 2218-2235 | **12** |
| `activities_json` | 2265-2283 | **10** |
| `activity_result` | 2284-2294 | **6** |

**89 uncovered statements**, out of 238 remaining in the file — the largest
single contiguous block left.

The first four are taken together because they are four near-identical answers
to one question — *serve this content object as ActivityPub JSON* — and
because, as in sub-projects 8, 9 and 10, the defects live in what they do
**differently**. `activities_json` and `activity_result` are the two
activity-log endpoints immediately below them: not content objects, but
contiguous, cheap, and one of them carries this slice's most serious finding.

Where the actor endpoints tell a remote instance *what an actor is* and the
collections tell it *what is attached to that actor*, these tell it *what a
post or comment actually says*. They are the endpoints a remote instance hits
when a user clicks through to content that originated here.

### Citations drift

Every sub-project since 5c found its brief's line numbers stale, and this
slice's own fix will move things. **Locate every target by code content.**
Quoted lines are navigational aids, never identifiers.

## The asymmetries — this slice's whole point

Read side by side, the four content-object endpoints disagree on nearly every
axis. Each row is a candidate defect, and this table is the spec's core claim.

| Axis | `comment_ap` | `post_ap` | `post_replies_ap` | `post_ap_context` |
|---|---|---|---|---|
| Route methods | `GET, HEAD` | `GET, HEAD, POST` | `GET` | `GET` |
| `HEAD` branch in body | reachable | reachable | **dead** | **dead** |
| Lookup | `PostReply.query.get_or_404` | `Post.query.get_or_404` | same | same |
| `local_only` / `private` | 403 | 403 | **none** | **none** |
| `status` guard | none | `< POST_STATUS_PUBLISHED` → 403 | **none** | **none** |
| `deleted` guard | **none** | none | **none** | 404 |
| Instance-block guard | 401 | 401 | **none** | **none** |
| Remote object | serves it | 301 to `ap_id` | serves it | serves it |
| Non-AP request | `continue_discussion` | `show_post` | **falls off the end** | `abort(400)` |
| `Cache-Control` | 120 | 120 | 15 | 15 |
| `Vary` | `Accept` / `Accept, User-Agent` | same | `Accept` | `Accept` |
| `Link` alternate | yes | yes, slug or `/post/<id>` | none | none |

`post_ap` is the only one of the four that is fully guarded. `post_replies_ap`
is guarded by nothing at all.

## Goal

Full statement coverage of all six, and branch coverage sufficient that no
guard survives having any one of its conjuncts dropped. Expected effect:
`app/activitypub/routes.py` moves from its measured **86.2221%** blended toward
**90%**, and the floor rises from 86 to the measured figure rounded down.

## Out of scope

The delivery cluster — `process_delete_request` (21 uncovered, a
`@celery.task`) and `announce_activity_to_followers` (19) — belongs with the
inbox work of sub-projects 5a-7, not with the outbound read surface. The
instance-metadata cluster — `lemmy_federated_instances` (17),
`api_is_ip_banned` (9), `api_is_email_banned` (9), `domain_blocks` (7) at
lines 283-364 — is a different kind of surface and a natural later slice.
`quote_boost_auth` (10) and `activitypub_external_interaction` (5) are an
interaction-authorisation pair, also later.

The delegates these call — `post_to_page`, `comment_model_to_json`,
`post_replies_for_ap`, `continue_discussion`, `show_post` — are doubled here,
not tested here.

## The defects this slice must confront

This slice carries the same bounded fix authorisation 5c through 10 had:
**defects found inside these six functions are fixed test-first, each in its
own commit, separate from every test-only commit, and each proved by a mutation
that fails a named test.** Anything larger is registered.

### Authorised for fixing

**`post_replies_ap` returns HTTP 500 to any browser.** Its whole body sits
inside `if (request.method == 'GET' or request.method == 'HEAD') and
is_activitypub_request():` and there is no `else`. A request without an
ActivityPub `Accept` header falls off the end, the view returns `None`, and
Flask raises. This is the **fourth** instance of the crash class sub-project 10
fixed three times — and `post_ap_context`, twelve lines below, shows the
correct shape (`else: abort(400)`) in the same file.

The fix is `else: abort(400)`, matching `post_ap_context` rather than inventing
a fifth spelling. `comment_ap` and `post_ap` both delegate to an HTML renderer
instead, which is a richer answer, but adopting it here would mean choosing a
renderer for a replies collection that has no HTML view — a larger change than
this slice authorises.

### Registered by default; the plan may propose fixes

**`activity_result` returns internal exception strings to any caller.** On a
failed activity it returns `jsonify({'error': activity.result, 'message':
activity.exception_message})`. `ActivityPubLog.exception_message` is populated
from caught exceptions, so a remote instance — or anyone who can guess an
activity id — receives this instance's internal error text. Registered rather
than fixed because the right replacement (a generic error, a logged reference,
nothing at all) is a decision about what peers are told, which is the line this
campaign draws for registration. **It is nonetheless the most serious finding
in this slice** and should be registered as such, not buried in a list.

**`post_replies_ap` has no visibility guard whatsoever.** No `deleted`, no
`local_only`, no `private`, no `status`. `post_ap` checks all four before
serving the same post's content; `post_replies_ap` enumerates that post's
replies to anyone. The reply URLs it returns are the same ones
`post_ap_context` gates behind `post.deleted`.

**`post_ap_context` checks only `deleted`.** It publishes `post.ap_id` and up
to 2000 reply `ap_id`s, plus `post.title` as `name` and the community's
`profile_id()`, with no `local_only`, `private` or `status` check. So a
local-only community's post titles and reply URIs are served to any caller.

**`comment_ap` never checks `reply.deleted`**, and never checks whether the
reply is local. `post_ap` redirects a remote post 301 to its `ap_id`;
`comment_ap` re-serves a remote reply's JSON as though this instance were
authoritative for it.

**Two dead `HEAD` branches.** `post_replies_ap` and `post_ap_context` are both
registered `methods=['GET']`, and both bodies test `request.method == 'HEAD'`
and build an empty collection for it. Flask never routes a HEAD to them, so
neither branch can execute. Either the route list or the branch is wrong.

**`find_instance_id()` writes to the database from a GET.** Both `comment_ap`
and `post_ap` call `find_instance_id(requestor_domain())` on every ActivityPub
GET. When the domain is unknown, `find_instance_id`
(`app/activitypub/util.py:2064-2080`) constructs an `Instance` row, adds it and
commits. So an unauthenticated read has a write side effect, and any caller can
create arbitrary `instance` rows by varying a `User-Agent`. Larger than these
six functions, so registered — but it is reached from two of them and the tests
here will demonstrate it.

**The instance-block guard is inert for ordinary clients.**
`requestor_domain()` (`app/utils.py:5736`) returns `''` unless the `User-Agent`
contains a `+`; `find_instance_id('')` returns `None`; and
`has_blocked_instance(None)` returns `False`. So the 401 branch in `comment_ap`
and `post_ap` fires only for a caller whose User-Agent carries a
`+https://domain` suffix. Whether that is sufficient is a federation question;
that it is undocumented is the finding.

**Four different `Cache-Control` max-ages** across four documents of the same
kind (120, 120, 15, 15), plus 2400 on `activities_json` — the same unexplained
spread D180 registered for the collections.

**`activities_json` is `@cache.cached(timeout=2400)`** and builds its own
`Cache-Control: public, max-age=2400`. Under test the decorator is inert
(`CACHE_TYPE='NullCache'`), so the tests measure the view, not the cache.

## Testing approach

**Entry.** `app.test_client()`, driving `GET /comment/<id>`, `/post/<id>`,
`/post/<id>/replies`, `/post/<id>/context`, `/activities/<type>/<id>` and
`/activity_result/<path>` with and without an ActivityPub `Accept` header.
This is the harness sub-projects 8, 9 and 10 proved.

**`is_activitypub_request()` is the switch every test turns.** It is
`'application/ld+json' in Accept or 'application/activity+json' in Accept`
(`app/activitypub/util.py:2200`) — note there are two byte-identical
definitions of this function in the codebase and the one in `app/utils.py` has
no importers. Tests set a real header rather than doubling the function.

**`requestor_domain()` is driven with a real `User-Agent`**, following
sub-project 8's precedent: the parse is part of what is under test, and the
401 branch cannot be reached without it.

**Doubling.** `post_to_page`, `comment_model_to_json`, `post_replies_for_ap`,
`continue_discussion` and `show_post` are all imported into
`app.activitypub.routes` (the first three at `:13-23`, the last two at `:32`)
and patched **there**, following the campaign's binding-site convention.
`show_post` and `continue_discussion` render templates and must not run.

**Seeding.** `make_post`, `make_post_reply`, `make_community`, `make_user`,
`make_instance`, `make_instance_block` and `seed_community_owner` all exist.
Note `make_post` takes neither `status` nor `deleted` and leaves `status` at
`POST_STATUS_PUBLISHED`; `make_post_reply` sets `deleted=False` explicitly.
Tests that depend on either set it explicitly.

**One new factory IS needed.** `ActivityPubLog` has no factory, and both
`activities_json` and `activity_result` require rows — the latter needs
`result` and `exception_message` set to distinguish its two branches. The plan
adds it. This is stated as a confirmed need, checked against the model, because
sub-project 8's spec claimed no factory was needed and was wrong.

**Every test asserts `response.status_code`**, and success paths also assert
`content_type`, `Cache-Control`, and — where the endpoint sets them — `Vary`
and `Link`. `Vary` is never absent and never bare: Flask-Compress appends
`Accept-Encoding` to every response, so the `Accept, User-Agent` branch is
observed as `Accept, User-Agent, Accept-Encoding`.

**A crash is an exception, not a 500 response.** `tests/conftest.py` sets
`TESTING=True` with no `PROPAGATE_EXCEPTIONS` override, so Flask re-raises and
the test client lets the exception escape. `post_replies_ap`'s pin therefore
asserts `pytest.raises(TypeError, match='did not return a valid response')`,
not `status_code == 500`. Harness fact 43 records this; sub-project 10 lost a
brief to assuming otherwise.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped separately, each killed by a distinct named test. Carry forward the
patterns this campaign has hit repeatedly:

- **A filter or attribute whose value equals what the factory always produces
  cannot be killed** by any test using that factory unmodified.
- **An assertion comparing a response value to the object's own column is
  vacuous whenever the factory never sets that column** — harness fact 50, and
  the reason a sub-project 10 test compared `[None] == [None]` and passed.
- **A guard behind an earlier `abort` is never reached**, so its mutation kills
  nothing.
- **A clause duplicated across two call sites has two ways to be wrong.**
  Mutate one site at a time.

**Docstrings must be true**, including after the fix. After inverting the
`post_replies_ap` pin, check every claim in the file about how many of these
endpoints crash — sub-project 10 lost three rounds to counts and cross-
references falsified by its own later tasks.

**One pytest session at a time**, and stopping `run_tests.sh` on the host does
not kill pytest in the container.

## New test file

One new file, `tests/test_ap_content_objects.py`. Six endpoints across 89
statements is one coherent unit sharing a harness, a seeding surface and the
cross-endpoint comparisons this slice exists to make. Splitting posts from
comments would hide exactly those comparisons.

## Global constraints

- Defects found in these six functions are fixed test-first, each in its own
  commit, each proved by mutation. Anything outside them is registered.
- Findings are numbered from **D187**, and **both** of the register's "Next free
  number" notes are updated in the same change.
- The coverage floor rises to the measured blended figure rounded down.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass. Only the controller runs it, one session at a time,
  and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository
  root is not the campaign's.

## Success criteria

1. All six functions reach full statement coverage.
2. No guard survives any one conjunct being dropped, each kill by a distinct
   named test.
3. Every test asserts the status code; success paths also assert the response
   headers that form the federation contract.
4. `post_replies_ap`'s 500 is pinned, then fixed, with a witnessed pre-fix
   failure and a mutation proof.
5. Every asymmetry in the table above is either fixed with a mutation-proved
   test or registered with a stated reason.
6. `activity_result`'s exception-message disclosure is registered explicitly,
   as this slice's most serious finding.
7. `coverage_floors.ini` raised for `app/activitypub/routes.py`.
8. The findings register carries every defect found, from D187.
9. Full suite green.
