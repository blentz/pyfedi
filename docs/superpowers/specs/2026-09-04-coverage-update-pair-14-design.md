# Sub-project 14: the update pair's mirrored core

**Date:** 2026-09-04
**Module:** `app/activitypub/util.py` — `update_post_from_activity`,
`update_post_reply_from_activity`
**Predecessors:** 5a-7 covered the inbox dispatcher; 8 webfinger; 9 the actor
profiles; 10 the collections; 11 the content objects; 12 the moderation and
ban-removal cluster; 13 the refresh-profile trio. 8-11 took
`app/activitypub/routes.py` from 35% to 90.8%; 12 and 13 took `util.py` from
48.9% to **61.8397%**.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D236**

## The unit

Two functions, one mirrored pair, and this slice takes the half of them that
is genuinely mirrored.

| Function | Lines at writing | Uncovered | In scope |
|---|---|---|---|
| `update_post_reply_from_activity` | 3005-3126 | **64** | all of it |
| `update_post_from_activity` | 3127-3549 | 151 | **`:3129-3254` only, 44 uncovered** |

**108 uncovered statements.**

These are the functions a remote peer drives by sending an `Update`. Every
branch in the scoped region is reachable by a peer choosing what to put in
`request_json['object']`.

### Why the pair is split rather than taken whole

`update_post_from_activity` continues past `:3254` into type-specific tails —
`Video` likes/dislikes collection fetches, `Question` poll rebuild-vs-totals,
`Event` field application, the Link/Document/Audio/Image attachment walk,
`POST_TYPE` detection with `make_image_sizes`, the moderator and admin
"suspicious content" notifications, and cross-post recalculation. Roughly 100
statements, and **none of it is mirrored** — the reply function has no
counterpart to any of it.

The comparison between siblings is what has found every defect in this
campaign since sub-project 9. Applied to unmirrored code it does not apply at
all, so those tails are slower per statement and belong in their own slice.

Sub-project 13 took 310 statements, finished 124 short, and **missed its own
first success criterion**. This spec is scoped so that criterion is reachable.

**The tails are sub-project 15.** They are named here so that nobody reads
their absence as an oversight.

## The asymmetries — this slice's whole point

Verified against source while scoping. Line numbers are navigational aids;
locate by content.

| Axis | `update_post_from_activity` | `update_post_reply_from_activity` |
|---|---|---|
| `source['mediaType']` read | **unguarded** (`:3134`) | `'mediaType' in …` checked first (`:3013`) |
| `content` is `None` | guarded (`:3131`) | **unguarded** (`:3008`) |
| object-level `mediaType` | `text/html` / `text/markdown` elif chain | **absent** |
| language source | `language` dict **or** `contentMap` (`:3190`) | `language` dict only |
| language applied | only when the id changed | unconditionally |
| tag list gate | `isinstance(…, list)` (`:3196`) | `… and len(tag) > 1` (`:3058`) |
| `json_tag['type']` read | **unguarded** at `:3202` and `:3208`, guarded at `:3214` | guarded (`:3060`) |
| Mention locale | `_()`, the sender's locale | `force_locale(get_recipient_language(...))` (`:3115`) |
| Mention de-duplication | none | four separate suppression rules |
| lock timeout / blocking | 60 / 60 (`:3129`) | 10 / 6 (`:3007`) |

Both functions share an outer shape: a `redis_client.lock` on the row, then a
sequence of `if <key> in request_json['object']:` applications, then a commit.

## Goal

Full statement coverage **of the scoped region**, and branch coverage
sufficient that no guard in it survives having any one of its conjuncts
dropped. Expected effect: `app/activitypub/util.py` moves from its measured
**61.8397%** blended upward, and the floor rises from 61 to the measured
figure rounded down.

The criterion is stated against the scoped region rather than against whole
functions, deliberately. Sub-project 13's criterion was stated against whole
functions, was not met, and the miss was only discovered at the final review.

## Out of scope

- **`update_post_from_activity` from `:3256` onward** — the type tails listed
  above. Sub-project 15.
- The rest of `app/activitypub/util.py`: `make_image_sizes_async` (152
  uncovered), `create_post_reply` (83), `process_report` (76),
  `new_instance_profile_task` (66), the notify pair (`notify_about_post_task`
  48, `notify_about_post_reply` 26), `undo_vote` (28).
- **The refresh trio's residual 124 statements**, registered as D235 and still
  open. That slice remains available and is entangled with
  `make_image_sizes_async`, which is why it is not this one.
- `markdown_to_html`, `allowlist_html`, `html_to_text`, `find_language`,
  `find_language_or_create`, `find_hashtag_or_create`, `find_flair_or_create`,
  `blocked_users` and `get_recipient_language` are driven or doubled here, not
  tested here.

## Existing suites — read these before writing anything

**Nine test files already drive these two functions.** Sub-project 9 wasted a
task by ignoring an existing partial suite; sub-project 13 avoided that by
naming the one file that mattered. This slice has nine:

| File | Tests | What it covers |
|---|---|---|
| `tests/test_unparseable_url_ingress.py` | 33 | rejecting a url `urlparse` cannot read at ingress, so it is never stored -- the second half of a two-layer defence whose first half guards the render path |
| `tests/test_inbox_dispatch_new_content.py` | 29 | `Create`/`Update` reaching these functions from the inbox |
| `tests/test_inbox_dispatch_create_update.py` | 28 | the inbox dispatcher's `Create`/`Update` routing, which is how these two functions are reached in production |
| `tests/test_url_helpers_accept_none.py` | 16 | every url-shaped helper answering for `None` rather than raising, since `update_post_from_activity` stores `post.url = None` without resetting `post.type` |
| `tests/test_federated_event_url_sentinel.py` | 4 | the `new_url = old_url if post.type == POST_TYPE_EVENT` sentinel |
| `tests/test_event_post_type_survives_update.py` | 5 | an `Event` surviving an `Update` that touches neither url nor image |
| `tests/test_dillo_video_teaser.py` | 3 | a `Video` type tail case |
| `tests/test_post_url_cleared_by_update.py` | 2 | url clearing |
| `tests/test_post_edit_null_url.py` | 2 | null url on edit |

**The plan reads all nine and reports what each already pins**, so this slice
adds coverage rather than duplicating it. Several sit in the out-of-scope
tails and are listed so the plan can recognise them and move on.

The campaign-authored comment blocks inside `update_post_from_activity` — the
`url_is_parseable` rationale and the `new_url = old_url` event sentinel — were
written by earlier slices. **They are load-bearing documentation of decisions
already made. Do not restate, contradict or delete them.**

## The defects this slice must confront

Same bounded fix authorisation sub-projects 5c through 13 carried: **defects
found inside these two functions are fixed test-first, each in its own commit,
separate from every test-only commit, and each proved by a mutation that fails
a named test.** Anything larger is registered.

Sub-project 13 refined that authorisation into a rule this slice inherits:
**a defect is fixed when the correct spelling already exists in the file and
the change is mechanical; it is registered when the fix would require choosing
new behaviour for a case the codebase has never handled.**

### Authorised for fixing

**`update_post_from_activity` crashes on a `source` object with no
`mediaType`.** The sibling checks membership first; this one subscripts
directly. A peer sending `"source": {"content": "..."}` raises `KeyError`. One
of two right, and the correct spelling is 120 lines away in the same file.

**`update_post_reply_from_activity` crashes on `"content": null`.** The
sibling guards `is not None`; this one calls `.startswith` on the value,
raising `AttributeError`. Again one of two right, and again remotely
triggerable.

**`update_post_from_activity` subscripts `json_tag['type']` unguarded twice
and guards it once, in the same loop.** `:3202` and `:3208` read the key
directly; `:3214`, eight lines below, checks `'type' in json_tag` first. A tag
object without a `type` raises `KeyError`. This is stronger evidence than a
pair disagreement — the same loop disagrees with itself, and the reply
function's loop agrees with the guarded spelling.

Each fix takes the shape already present. **Match it rather than inventing a
fourth spelling.**

### Carried forward from sub-project 13, and fixed here

Both were registered by 13 and both meet the fix rule. Each gets its own
commit, test-first, mutation-proved, exactly as if found here.

- **D232 — the following-collection fetch sends no `Accept` header.** It is
  the only `get_request` in the refresh trio that omits
  `headers={'Accept': 'application/activity+json'}`. A peer that
  content-negotiates on `/following` serves HTML, which now reaches
  sub-project 13's own decode guard and returns silently — so 13's crash fix
  converted a loud failure into a feed that quietly stops syncing. That is
  what makes this the more urgent of the two.
- **D219(c) — the feed owners removal loop does not unwrap dict entries**
  where the community moderators loop does. Two lines, copied verbatim from a
  loop in the same file. Sub-project 13's final review judged it to meet 13's
  own fix rule and to have been registered inconsistently.

### Registered by default; the plan may propose fixes

- **`update_post_reply_from_activity` ignores a lone Mention.** Its tag gate
  requires `len(request_json['object']['tag']) > 1`, so a peer sending exactly
  one tag — a single `Mention` — is skipped entirely. The post function's gate
  has no length condition. Registered rather than fixed because changing it
  changes which notifications this instance generates, which is behaviour
  rather than a guard.
- **Only the reply path localises the notification to the recipient.** The
  post path builds its title with `_()`, under whatever locale the request is
  being handled in; the reply path wraps the same construction in
  `force_locale(get_recipient_language(recipient.id))`. A local user mentioned
  in a post can therefore be notified in another user's language.
- **Only the reply path de-duplicates Mentions**, with four suppression rules:
  the post author, a Mention already notified from the post body, a Mention
  already notified anywhere in the comment chain, and a recipient who authored
  a comment further up that chain. The post path has none.
- **Only the post path reads `contentMap`** as a language fallback, and only
  the post path compares against the existing `language_id` before assigning.
- **Only the post path handles an object-level `mediaType`.**
- **The two locks differ by 6x and 10x** — 60/60 against 10/6 — with no stated
  reason. A slow `Update` on a reply gives up where the same work on a post
  waits.

## Testing approach

**Entry: direct function call**, with a hand-built `request_json`. Both
functions take an already-loaded row and a dict; neither fetches the object it
is applying, so no HTTP mock is needed for the scoped region. This is a
cheaper harness than the refresh trio's and the plan should rely on it.

**`redis_double` will NOT work here, and the plan must not reach for it.**
Both functions open with `with redis_client.lock(...)`, and
`tests/conftest.py`'s `redis_double` docstring records why that fails: with no
`lupa` installed, fakeredis implements no Lua scripting, so `Lock.acquire()`
succeeds on plain `SET NX PX` but `Lock.release()` issues an `EVALSHA` and
raises `redis.exceptions.ResponseError: unknown command 'evalsha'` on
`__exit__` — every time, for every one of the 34 `redis_client.lock(` call
sites under `app/`.

The remedy already exists and is named in that same docstring: a narrow local
double whose `.lock(...)` returns `contextlib.nullcontext()`. See
`_RedisLockOnlyDouble` / `redis_lock_only_double` in
`tests/test_inbox_dispatch_votes.py`, and "The fakeredis lock limitation" in
`tests/README.md`. **The plan reads both and reuses that double rather than
inventing a third.** Both functions do `from app import redis_client` inside
the function body, so the double must be bound where that import resolves it.

This is the single harness fact most likely to cost this slice a task if it is
rediscovered rather than read.

**`db.session` is the session under test here**, unlike the refresh trio,
which used `get_task_session()`. These two functions commit on `db.session`
directly. `expire_on_commit` is at its default `True` — the app factory
overrides only `autoflush` — so a commit inside the function expires the
test's objects and attribute access re-loads them. **The plan states which of
its assertions depend on that.**

**Every test asserts on persisted row state**, not merely on the absence of an
exception. Both functions apply many independent fields before committing, so
a guard that abandoned the whole `Update` would pass a "nothing raised" test.

**No vacuous assertions.** Never assert a value equal to a column's declared
default without a contrary baseline. `Post.nsfw`, `Post.nsfl`,
`PostReply.distinguished` and `PostReply.replies_enabled` all have declared
defaults, and each is written by a branch in scope — every one of those tests
seeds the opposite value first.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped separately, each killed by a distinct named test. Carry forward what
the campaign has recorded, and in particular the two lessons sub-project 13
paid for:

- **Mutating a whole guard to `if True:` is a site-level proof, not a
  conjunct-level one.** When a fix adds a conjunct, the mutation must delete
  that conjunct alone. The failure is self-concealing: the site-level mutant
  does die, so the table shows a green kill and the missing proof leaves no
  trace.
- **When two guards sit in sequence, the pin for the outer one must serve a
  payload the inner one accepts**, or both mutations kill by the same
  exception and the guards become indistinguishable.
- A conjunct whose excluded set is empty under the fixture is unkillable; the
  remedy is a fixture that creates the row the guard exists to exclude, never
  a weaker assertion.
- A conjunct can be **sole-killable only by crash**, and that is a legitimate
  kill — but "it only crashes" is a finding only once no non-crashing input
  has been shown to exist.

**Docstrings must be true**, including after the fixes, and including counts.
Sub-project 13's final review found seven false docstrings after every task
had passed its own review. The lesson it drew is binding here: **a docstring
sentence that describes production structure in prose rather than in
identifiers cannot be audited by grep, and must be re-read against source
whenever the code it describes moves.** Counts and distances stated in prose
are the most fragile form and are best omitted.

**One pytest session at a time.** Any run over 600s is erroneous; the podman
stack degrades and `./run_tests.sh --down` restores it.

## New test file

One new file, `tests/test_ap_update_pair.py`. The nine existing files stay as
they are — each covers a different concern, several of them in the
out-of-scope tails, and merging would bury both.

## Global constraints

- Defects found in these two functions are fixed test-first, each in its own
  commit, each proved by mutation. Anything outside them is registered.
- The two carried-forward fixes, D232 and D219(c), each get their own commit
  on the same terms.
- Findings are numbered from **D236**, and **both** live "Next free number"
  notes are updated in the same change. The historical notes are frozen
  records — leave them alone.
- The coverage floor for `app/activitypub/util.py` rises to the measured
  blended figure rounded down. It currently reads 61.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass. Only the controller runs it, one session at a
  time, and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository
  root is not the campaign's.

## Success criteria

1. Full statement coverage of the **scoped region** — all of
   `update_post_reply_from_activity`, and `update_post_from_activity` from its
   lock through `post.edited_at`. Measured per-region and reported, not
   assumed.
2. No guard in the scoped region survives any one conjunct being dropped, each
   kill by a distinct named test.
3. Every test asserts on persisted row state, not merely on the absence of an
   exception.
4. All three crash paths — the unguarded `source['mediaType']`, the unguarded
   `content` `None`, and the unguarded `json_tag['type']` — are pinned, then
   fixed, each with a witnessed pre-fix failure and a mutation proof.
5. D232 and D219(c) are fixed on the same terms and their register entries
   updated to say so.
6. Every asymmetry in the table above is either fixed with a mutation-proved
   test or registered with a stated reason.
7. `coverage_floors.ini` raised for `app/activitypub/util.py`.
8. The findings register carries every defect found, from D236.
9. Full suite green.
